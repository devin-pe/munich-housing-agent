"""Spacest scraper.

WHY HTTP: Spacest is a Next.js site that ships the full search result set as
inline JSON in <script id="__NEXT_DATA__">. Listings include coordinates, so no
geocoding is needed. Spacest is a furnished mid-term platform, so the monthly
price is effectively all-inclusive (warm) and listings are furnished.

ROOMS: Spacest categorises by bedrooms via URL subpath, not German Zimmer. We
scrape the subpaths that map into our 1–2 Zimmer target and assign Zimmer:
    studios      -> 1 Zimmer
    one-bedroom  -> 2 Zimmer (1 BR + living room)
(two-bedrooms = 3 Zimmer is out of range, so we don't fetch it.)

Only self-contained apartments are kept (item type == 2); rooms in shared flats
(type == 1) and student-campus units (isCampus) are skipped.

COVERAGE LIMIT: Spacest's server-side pagination is broken — the ?page/?offset
params (and even the _next/data endpoint) always return the same first result set;
the full catalogue is only reachable via its authenticated API gateway. So we
capture roughly the first page of each subpath (studios + one-bedroom, ~20 unique
listings). This is a known cap, not a silent truncation.

FRAGILE BITS (update if the site changes):
    - SEARCH base path and the subpath→Zimmer map
    - JSON path props.pageProps.searchResponse.data.listings.{data,totalNumberPages}
    - detail URL pattern /rent-listing/<id>
"""
from __future__ import annotations

import json
import logging

from selectolax.parser import HTMLParser

from ..filters import is_student_only_text, is_women_only_text
from ..models import Listing
from .base import BaseScraper

logger = logging.getLogger("housing_agent")

BASE = "https://spacest.com"
TYPE_APARTMENT = 2
# subpath -> German Zimmer count (only those within a 1–2 Zimmer target)
SUBPATHS = {"studios": 1, "one-bedroom": 2}


class SpacestScraper(BaseScraper):
    name = "spacest"

    def _base_path(self) -> str:
        country = self.config.search.country.strip().lower().replace(" ", "-")
        city = self.config.search.city.strip().lower()
        return f"{BASE}/rent-listings/{country}/{city}/apartments"

    @staticmethod
    def _listings_block(html: str) -> dict | None:
        tree = HTMLParser(html)
        node = tree.css_first('script#__NEXT_DATA__')
        if not node:
            return None
        try:
            j = json.loads(node.text())
        except json.JSONDecodeError:
            return None
        try:
            return j["props"]["pageProps"]["searchResponse"]["data"]["listings"]
        except (KeyError, TypeError):
            return None

    def _to_listing(self, item: dict, zimmer: int) -> Listing | None:
        if item.get("type") != TYPE_APARTMENT:   # skip shared rooms
            return None
        # Skip student-only and women-only listings: the campus flag misses many
        # (e.g. named "… Studenten" with the restriction only in the description),
        # so check the name + description text too.
        name, desc = item.get("name"), item.get("description")
        if item.get("isCampus") or is_student_only_text(name, desc) or is_women_only_text(name, desc):
            return None
        lid = item.get("id")
        if not lid:
            return None
        price = item.get("monthlyPrice") or item.get("price")
        currency = (item.get("currency") or {}).get("code", "EUR")
        if currency and currency != "EUR":
            return None
        addr = item.get("address") or {}
        area = addr.get("unformattedAddress") or ", ".join(
            p for p in [addr.get("addressRoute"), addr.get("addressCity")] if p
        ) or self.config.search.city.title()
        return Listing(
            source=self.name,
            url=f"{BASE}/rent-listing/{lid}",
            listing_id=str(lid),
            title=item.get("name") or "Spacest apartment",
            price_eur=float(price) if isinstance(price, (int, float)) else None,
            price_type="warm",   # furnished mid-term platform: all-inclusive
            rooms=zimmer,
            furnished=True,
            address_or_area=area,
            lat=addr.get("latitude"),
            lng=addr.get("longitude"),
            area_sqm=None,
        )

    def scrape(self) -> list[Listing]:
        listings: list[Listing] = []
        seen: set[str] = set()   # Spacest's ?page= param is unreliable (repeats a
        # result set) and studios/one-bedroom overlap, so dedupe by id globally.
        max_pages = max(1, self.source_cfg.max_pages)
        for sub, zimmer in SUBPATHS.items():
            base = f"{self._base_path()}/{sub}"
            for page in range(max_pages):   # Spacest pages are 0-indexed
                url = base if page == 0 else f"{base}?page={page}"
                try:
                    resp = self.get(url)
                except Exception as exc:
                    logger.warning("[spacest] %s page %d failed: %s", sub, page, exc)
                    break
                block = self._listings_block(resp.text)
                if not block:
                    break
                new = 0
                for item in block.get("data") or []:
                    lg = self._to_listing(item, zimmer)
                    if lg and lg.listing_id not in seen:
                        seen.add(lg.listing_id)
                        listings.append(lg)
                        new += 1
                # Stop paginating this subpath once a page yields no new listings
                # (handles the flaky ?page= param that repeats results).
                if new == 0:
                    break
        logger.info("[spacest] collected %d raw listings", len(listings))
        return listings
