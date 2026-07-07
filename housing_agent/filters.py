"""Central, source-agnostic filtering + German-rent normalization.

Handles the German rental nuances explicitly:
  * Warmmiete vs Kaltmiete: we filter on the *warm* price (rent incl. utilities).
    If a listing only advertises Kaltmiete, we estimate warm = kalt + Nebenkosten
    (configurable) and flag the listing as an estimate.
  * Zimmer counting: "1-Zimmer" = studio, "2-Zimmer" = 1BR+living. A 1-bedroom
    target is therefore 1–2 Zimmer (min_rooms/max_rooms in config).
  * möbliert (furnished): required when furnished_required is set.
"""
from __future__ import annotations

import logging
import re

from .config import Config
from .models import Listing

logger = logging.getLogger("housing_agent")

# Titles/text that indicate a student-only listing even when a structured flag is
# unset. Kept reasonably tight so "perfect for students and professionals" is NOT
# matched, while catching exclusive phrasing and student residences/dorms.
_STUDENT_ONLY_RE = re.compile(
    r"only\s+(?:for|available\s+for)\s+students"
    r"|students?\s+only"
    r"|nur\s+f(?:ü|ue)r\s+studenten"
    r"|studenten\s+only"
    r"|only\s+students"
    r"|student(?:en)?wohnheim"          # student dormitory (inherently student-only)
    r"|student\s+(?:residence|hall|dorm|housing\s+only)",
    re.I,
)


def _is_student_only(listing: Listing) -> bool:
    if listing.extra.get("only_students"):
        return True
    hay = f"{listing.title or ''} {listing.extra.get('price_label', '')}"
    return bool(_STUDENT_ONLY_RE.search(hay))


def compute_warm_price(listing: Listing, nebenkosten_estimate: float) -> None:
    """Populate listing.warm_price_eur and listing.price_is_estimated in-place."""
    if listing.price_eur is None:
        listing.warm_price_eur = None
        return
    if listing.price_type == "warm":
        listing.warm_price_eur = listing.price_eur
        listing.price_is_estimated = False
    elif listing.price_type == "kalt":
        listing.warm_price_eur = listing.price_eur + nebenkosten_estimate
        listing.price_is_estimated = True
    else:  # unknown — treat the advertised figure as warm but flag it
        listing.warm_price_eur = listing.price_eur
        listing.price_is_estimated = True


def apply_filters(listings: list[Listing], config: Config) -> tuple[list[Listing], dict]:
    """Keep only listings that satisfy price, rooms, and furnished criteria.

    Commute filtering is applied separately (commute.py). Returns (kept, stats).
    """
    s = config.search
    stats = {
        "input": len(listings),
        "dropped_price": 0,
        "dropped_rooms": 0,
        "dropped_furnished": 0,
        "dropped_no_price": 0,
        "dropped_student_only": 0,
        "dropped_end_date": 0,
        "kept": 0,
    }
    kept: list[Listing] = []

    for lg in listings:
        compute_warm_price(lg, s.nebenkosten_estimate_eur)

        # Student-only listings are irrelevant for a working professional
        # (checks both the structured flag and student-only phrasing in the title).
        if _is_student_only(lg):
            stats["dropped_student_only"] += 1
            continue

        # Exclude fixed-term listings that end before the required date (short
        # sublets). Only applies when the source exposed an end date; unknown =
        # treated as available indefinitely and kept.
        if s.min_available_until and lg.available_until and lg.available_until < s.min_available_until:
            stats["dropped_end_date"] += 1
            continue

        if lg.warm_price_eur is None:
            stats["dropped_no_price"] += 1
            continue
        if lg.warm_price_eur > s.max_price_eur:
            stats["dropped_price"] += 1
            continue

        # Rooms: keep if unknown (don't hide a listing for missing metadata), else
        # enforce the 1–2 Zimmer window.
        if lg.rooms is not None and not (s.min_rooms <= lg.rooms <= s.max_rooms):
            stats["dropped_rooms"] += 1
            continue

        if s.furnished_required and lg.furnished is False:
            stats["dropped_furnished"] += 1
            continue

        kept.append(lg)

    stats["kept"] = len(kept)
    logger.info("Attribute filter: %s", stats)
    return kept, stats
